SELECT
  firstname,
  lastname,
  city,
  state
FROM PERSON
LEFT JOIN ADDRESS
  ON address.personid = person.personid
WHERE
  NOT address.personid IS NULL OR city IS NULL