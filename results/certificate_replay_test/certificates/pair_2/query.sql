SELECT
  firstname,
  lastname,
  address.city,
  address.state
FROM PERSON
LEFT JOIN ADDRESS
  ON address.personid = person.personid