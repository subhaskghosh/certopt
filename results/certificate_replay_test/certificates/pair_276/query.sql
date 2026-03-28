SELECT DISTINCT
  person1.email
FROM PERSON AS PERSON1
JOIN PERSON AS PERSON2
  ON TRUE
WHERE
  person1.id <> person2.id AND person1.email = person2.email